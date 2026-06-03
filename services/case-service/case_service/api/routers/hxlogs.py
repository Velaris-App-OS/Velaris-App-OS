"""P63 — HxLogs: AI-driven unified log analyser.

Reads structlog output from all Helix service log files, serves
filtered entries to the Studio, and uses HxNexus to explain errors.
No DB tables — purely a log file reader.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from case_service.auth.dependencies import get_current_user
from case_service.auth.models import AuthenticatedUser

router = APIRouter(prefix="/hxlogs", tags=["hxlogs"])

# ── Service log file map ──────────────────────────────────────────

SERVICE_LOGS: dict[str, str] = {
    "case-service": "/tmp/velaris-case-service.log",
    "studio":       "/tmp/velaris-studio.log",
    "engine":       "/tmp/velaris-engine.log",
}

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")

# ISO-8601: 2026-05-12T15:52:58.835934Z  (engine / Temporal / structlog JSON)
TS_ISO = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)"
)
# Space-separated: 2026-05-12 15:52:58  (stdlib %(asctime)s default format)
TS_SPACE = re.compile(
    r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?)"
)

# Positional match only — level must appear at the START of a line (after optional
# timestamp) not embedded inside a message. Prevents "Server connection error: ..."
# from being misclassified as ERROR level.
SEVERITY_RE = re.compile(
    r"^(?:"                                                             # start of line
    r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\S*\s+"                  # optional timestamp
    r")?[\[\*]?\s*"                                                     # optional brackets
    r"(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|EXCEPTION)"     # the level
    r"[\s:\]\-#]",                                                      # must be followed by space/colon
    re.IGNORECASE,
)

TRACEBACK_START = re.compile(r"^(Traceback \(most recent call last\)|  \+ Exception Group Traceback)")
TRACEBACK_FRAME = re.compile(r'^  (File "|  \|.*File "|  \+.*File ")')


# Pattern A: "2026-05-13 14:23:01 INFO     message"  (our new stdlib format)
_PREFIX_WITH_TS = re.compile(
    r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:\d{2})?\s+"
    r"(?:DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|EXCEPTION)-?\w*\s*:?\s*",
    re.IGNORECASE,
)
# Pattern B: "INFO:     message"  (old uvicorn format, no timestamp)
_PREFIX_NO_TS = re.compile(
    r"^(?:DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|EXCEPTION)\s*:[-\s]*",
    re.IGNORECASE,
)


def _clean_message(raw_first_line: str) -> str:
    """Strip timestamp+level prefix from first line — handles both old and new formats."""
    clean = _strip_ansi(raw_first_line).strip()
    # Try timestamp+level prefix first
    result = _PREFIX_WITH_TS.sub("", clean).strip()
    if result != clean:
        return result
    # Fall back to level-only prefix (old uvicorn format)
    return _PREFIX_NO_TS.sub("", clean).strip()


def _strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def _fmt_ts(raw: str) -> str:
    """Normalise any timestamp string to 'YYYY-MM-DD HH:MM:SS'."""
    try:
        from datetime import datetime as _dt
        dt = _dt.fromisoformat(raw.replace("Z", "+00:00").replace("T", " ").split("+")[0].split("-0")[0])
        return dt.strftime("%Y-%m-%d  %H:%M:%S")
    except Exception:
        return raw[:19].replace("T", " ")


def _extract_timestamp(line: str) -> str | None:
    """Extract a human-readable timestamp from a log line.

    Tries in order:
    1. JSON structlog line  → {"timestamp": "..."}
    2. ANSI-wrapped ISO-8601 (engine/Temporal logs)
    3. Space-separated datetime (stdlib %(asctime)s format)
    """
    clean = _strip_ansi(line).strip()

    # 1. JSON structlog  {"timestamp": "2026-05-13T14:23:01.442Z", ...}
    if clean.startswith("{"):
        try:
            import json as _json
            obj = _json.loads(clean)
            ts = obj.get("timestamp") or obj.get("ts") or obj.get("time")
            if ts:
                return _fmt_ts(str(ts))
        except Exception:
            pass

    # 2. ISO-8601 anywhere in the line (handles ANSI-wrapped engine timestamps)
    m = TS_ISO.search(clean)
    if m:
        return _fmt_ts(m.group("ts"))

    # 3. Space-separated datetime (stdlib logging with %(asctime)s)
    m = TS_SPACE.search(clean)
    if m:
        return _fmt_ts(m.group("ts"))

    return None


class LogEntry:
    def __init__(self, service: str, raw: str, level: str, line_no: int):
        self.service = service
        self.raw = raw
        self.level = level.upper()
        self.line_no = line_no
        self.is_traceback = False
        self.frames: list[str] = []


def _parse_level(line: str) -> str:
    """Detect log level positionally — never from embedded words in the message body."""
    clean = _strip_ansi(line)
    m = SEVERITY_RE.search(clean)
    if m:
        return m.group("level").upper()
    if TRACEBACK_START.match(clean.strip()):
        return "ERROR"
    return "INFO"


def _read_logs(
    service: str,
    path: str,
    severity: Optional[str],
    limit: int,
    since_minutes: int,
) -> list[dict]:
    if not os.path.exists(path):
        return []

    entries: list[dict] = []
    current_block: list[str] = []
    current_level = "INFO"
    current_lineno = 0
    current_ts: str | None = None
    last_known_ts: str | None = None   # carry forward when a line has no timestamp
    in_traceback = False
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)

    try:
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []

    def flush_block(lineno: int) -> None:
        if not current_block:
            return
        raw = "".join(current_block).rstrip()
        is_tb = TRACEBACK_START.match(current_block[0]) is not None

        # Severity filter
        lvl = "ERROR" if is_tb else current_level
        if severity and lvl != severity.upper() and not (severity.upper() == "ERROR" and is_tb):
            current_block.clear()
            return

        # Extract frames from tracebacks
        frames: list[str] = []
        if is_tb:
            for bl in current_block:
                if 'File "' in bl:
                    frames.append(bl.strip())

        first_line = current_block[0] if current_block else raw.split("\n")[0]
        entries.append({
            "service":    service,
            "level":      lvl,
            "line_no":    lineno,
            "occurred_at": current_ts,
            "message":    _clean_message(first_line),   # timestamp+level stripped
            "raw":        raw,
            "is_traceback": is_tb,
            "frames":     frames,
            "innermost":  frames[-1] if frames else None,
        })
        current_block.clear()

    for i, line in enumerate(lines[-20_000:]):   # read last 20k lines at most
        clean_line  = _strip_ansi(line)
        is_tb_start = bool(TRACEBACK_START.match(clean_line))
        is_frame    = bool(TRACEBACK_FRAME.match(clean_line))
        looks_new   = bool(SEVERITY_RE.search(clean_line)) and not is_frame

        if looks_new or is_tb_start:
            flush_block(current_lineno)
            current_level  = _parse_level(line)
            extracted      = _extract_timestamp(line)
            # Use extracted ts if found, otherwise keep the last known one
            if extracted:
                last_known_ts = extracted
            current_ts     = extracted or last_known_ts
            current_lineno = i
            in_traceback   = is_tb_start

        current_block.append(line)

    flush_block(current_lineno)

    # ── Two-pass timestamp propagation ───────────────────────────
    # Pass 1 — forward: fill from the previous entry if this one has none
    for i in range(1, len(entries)):
        if entries[i]["occurred_at"] is None and entries[i - 1]["occurred_at"] is not None:
            entries[i]["occurred_at"] = entries[i - 1]["occurred_at"]
    # Pass 2 — backward: fill from the next entry for any still-missing ones
    for i in range(len(entries) - 2, -1, -1):
        if entries[i]["occurred_at"] is None and entries[i + 1]["occurred_at"] is not None:
            entries[i]["occurred_at"] = entries[i + 1]["occurred_at"]

    # Return last `limit` entries, newest last → reverse for newest-first
    filtered = [e for e in entries if
        severity is None or e["level"] == severity.upper() or
        (severity.upper() == "ERROR" and e["is_traceback"])
    ] if severity else entries

    return list(reversed(filtered[-limit:]))


# ── Endpoints ─────────────────────────────────────────────────────

@router.get("/services")
async def list_services(_: AuthenticatedUser = Depends(get_current_user)):
    """List all known service log files and their availability."""
    result = []
    for name, path in SERVICE_LOGS.items():
        exists = os.path.exists(path)
        size   = os.path.getsize(path) if exists else 0
        result.append({
            "service": name, "path": path,
            "available": exists, "size_bytes": size,
        })
    return {"services": result}


@router.get("/entries")
async def get_log_entries(
    service:       Optional[str] = Query(None, description="Filter by service name (omit for all)"),
    severity:      Optional[str] = Query(None, description="ERROR | WARNING | INFO | DEBUG"),
    since_minutes: int           = Query(60, description="How many minutes back to look"),
    limit:         int           = Query(200, le=1000),
    _:             AuthenticatedUser = Depends(get_current_user),
):
    """Return filtered log entries from one or all services."""
    services_to_read = (
        {service: SERVICE_LOGS[service]} if service and service in SERVICE_LOGS
        else SERVICE_LOGS
    )

    all_entries: list[dict] = []
    for svc, path in services_to_read.items():
        all_entries.extend(_read_logs(svc, path, severity, limit, since_minutes))

    # Sort newest first, cap at limit
    all_entries.sort(key=lambda e: e["line_no"], reverse=True)
    return {
        "entries": all_entries[:limit],
        "total":   len(all_entries),
        "services": list(services_to_read.keys()),
    }


class AnalyseRequest(BaseModel):
    log_text: str
    context: Optional[str] = None   # extra context (case ID, component name, etc.)


@router.post("/analyse")
async def analyse_log(
    body: AnalyseRequest,
    _:    AuthenticatedUser = Depends(get_current_user),
):
    """Send a log block or traceback to HxNexus for AI root-cause analysis."""
    from case_service.hxnexus.factory import generate_json

    prompt = (
        f"Analyse this Helix platform log output and identify the root cause:\n\n"
        f"```\n{body.log_text[:4000]}\n```\n\n"
        + (f"Additional context: {body.context}\n\n" if body.context else "")
        + "Return JSON:\n"
        '{"summary": "one-line plain English summary of what failed", '
        '"root_cause": "the specific cause", '
        '"likely_file": "filename:line if determinable else null", '
        '"suggested_fix": "concrete actionable fix", '
        '"severity": "critical|high|medium|low", '
        '"related_components": ["list", "of", "affected", "helix", "modules"]}'
    )

    result = await generate_json(
        prompt,
        system="You are a senior Helix platform engineer analysing production logs. Be precise and actionable.",
    )

    if not result:
        # Fallback: extract the most useful line without AI
        lines = body.log_text.strip().split("\n")
        error_lines = [l for l in lines if "Error" in l or "Exception" in l or "error" in l.lower()]
        return {
            "summary": error_lines[0].strip() if error_lines else "Could not parse log",
            "root_cause": "AI unavailable — check log manually",
            "likely_file": None,
            "suggested_fix": "Enable HxNexus AI backend for detailed analysis",
            "severity": "unknown",
            "related_components": [],
            "ai_available": False,
        }

    return {**result, "ai_available": True}


@router.post("/correlate")
async def correlate_with_hxstream(
    case_id:        Optional[str] = None,
    event_id:       Optional[str] = None,
    window_minutes: int = 5,
    _: AuthenticatedUser = Depends(get_current_user),
):
    """Find log entries in the same time window as an HxStream error event."""
    # Grab recent errors across all services that could relate to the case
    all_entries: list[dict] = []
    for svc, path in SERVICE_LOGS.items():
        entries = _read_logs(svc, path, "ERROR", 100, window_minutes * 2)
        all_entries.extend(entries)

    filtered = all_entries
    if case_id:
        filtered = [e for e in all_entries if case_id in e["raw"]]

    return {
        "case_id":   case_id,
        "event_id":  event_id,
        "window_min": window_minutes,
        "correlated_entries": filtered[:50],
    }
