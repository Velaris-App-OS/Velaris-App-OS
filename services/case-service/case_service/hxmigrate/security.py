"""HxMigrate security utilities — SEC-6, SEC-8."""
from __future__ import annotations

import re

# Patterns that look like secrets — redact before logging or persisting
_SECRET_PATTERNS = [
    re.compile(r'[A-Za-z0-9_\-]{20,}'),   # generic long tokens (Anthropic, OpenAI, JWT segments)
    re.compile(r'sk-[A-Za-z0-9]{20,}'),    # OpenAI-style keys
    re.compile(r'Bearer\s+\S+'),            # Bearer tokens in error text
    re.compile(r'[A-Za-z_][A-Za-z0-9_]*=[^\s,;]{8,}'),  # KEY=value env assignments
    re.compile(r'/home/[^/\s]+'),           # home directory paths
    re.compile(r'postgresql://[^\s]+'),     # DB connection strings
    re.compile(r'redis://[^\s]+'),
]

# Only these characters are allowed in platform names, run names, modes
_SAFE_NAME_RE = re.compile(r'^[A-Za-z0-9 _\-\.]{1,200}$')
_SAFE_SLUG_RE = re.compile(r'^[a-z0-9_\-]{1,100}$')

SUPPORTED_PLATFORMS = frozenset({
    "pega", "camunda", "appian", "servicenow",
    "jbpm", "flowable", "ibm", "oracle", "bizagi",
    "power_automate", "salesforce", "nintex",
})

MAX_UPLOAD_BYTES = 100 * 1024 * 1024        # 100 MB
MAX_JSONB_BYTES  = 50  * 1024 * 1024        # 50 MB — per JSONB column
MAX_XML_BYTES    = 10  * 1024 * 1024        # 10 MB — per individual XML file
MAX_JSON_BYTES   = 10  * 1024 * 1024        # 10 MB — per individual JSON file
MAX_ZIP_TOTAL    = 256 * 1024 * 1024        # 256 MB — total uncompressed
MAX_ZIP_ENTRY    = 50  * 1024 * 1024        # 50 MB — per ZIP entry
MAX_ZIP_FILES    = 2000                     # max entries in archive
MAX_JSON_DEPTH   = 50                       # max nesting depth


def sanitize_error(msg: str) -> str:
    """Strip secret-looking patterns from error messages before DB/API exposure (SEC-8)."""
    for pattern in _SECRET_PATTERNS:
        msg = pattern.sub("[REDACTED]", msg)
    return msg[:1000]


def validate_platform(platform: str) -> bool:
    """SEC-10: exact allowlist check for source_platform."""
    return platform.lower().strip() in SUPPORTED_PLATFORMS


def validate_safe_name(name: str) -> bool:
    """SEC-10: name fields — alphanumeric, spaces, hyphens, underscores, dots only."""
    return bool(_SAFE_NAME_RE.match(name))


def check_json_depth(obj: object, current: int = 0) -> int:
    """Return max depth of a parsed JSON object. Raises ValueError if > MAX_JSON_DEPTH."""
    if current > MAX_JSON_DEPTH:
        raise ValueError(f"JSON nesting depth exceeds limit ({MAX_JSON_DEPTH})")
    if isinstance(obj, dict):
        if not obj:
            return current
        return max(check_json_depth(v, current + 1) for v in obj.values())
    if isinstance(obj, list):
        if not obj:
            return current
        return max(check_json_depth(v, current + 1) for v in obj)
    return current
