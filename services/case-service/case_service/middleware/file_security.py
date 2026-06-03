"""File security: allowlist for uploads, blocklist for sensitive filenames.

Protects against:
- Upload of config/secret files (.env, *.key, *.pem, secrets.*)
- Path traversal in filenames (../, ./)
- Serving raw source files through download endpoints

Used in two ways:
1. validate_upload_filename()  — call in any upload handler before saving
2. BLOCKED_EXTENSIONS / BLOCKED_NAMES — import constants where needed
"""
from __future__ import annotations

import os
import re

# ── Upload allowlist ──────────────────────────────────────────────────────────
# Only these extensions are accepted in document/BPM import uploads.
# Add to this list when a new upload type is introduced — never remove.

ALLOWED_DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".rtf", ".csv", ".odt", ".ods", ".odp",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".tiff",
    ".mp4", ".mp3", ".wav",
    ".zip", ".tar", ".gz",
    ".json", ".xml",
}

ALLOWED_BPM_IMPORT_EXTENSIONS = {
    ".jar", ".zip", ".xml", ".bpmn",
}

ALLOWED_BPMN_EXTENSIONS = {
    ".bpmn", ".xml",
}

# ── Blocklist — these must NEVER be uploaded or served ────────────────────────

BLOCKED_EXTENSIONS = {
    ".env", ".key", ".pem", ".crt", ".cer", ".p12", ".pfx",
    ".pub", ".ppk", ".der",          # crypto / certificates
    ".py", ".pyc", ".pyo",           # source code
    ".sh", ".bash", ".zsh",          # shell scripts
    ".exe", ".dll", ".so", ".dylib", # binaries
    ".cfg", ".ini", ".conf",         # generic config
    ".sql",                          # raw SQL
    ".log",                          # log files
}

BLOCKED_FILENAME_PATTERNS = [
    r"^\.env",                # .env, .env.local, .env.production …
    r"^secrets?\.",           # secrets.json, secret.yaml …
    r"^\.git",                # .gitconfig, .git-credentials …
    r"config\.(py|js|ts|yaml|yml|json|toml)$",
    r"settings\.(py|json|yaml|yml)$",
    r"credentials?\.",
    r"password",
    r"private[_\-]?key",
    r"api[_\-]?key",
    r"\.htpasswd",
    r"wp-config",
    r"database\.yml",
]

_BLOCKED_RE = [re.compile(p, re.IGNORECASE) for p in BLOCKED_FILENAME_PATTERNS]

# ── Path traversal detection ──────────────────────────────────────────────────

_TRAVERSAL_RE = re.compile(r"\.\.[/\\]|^[/\\]")


def validate_upload_filename(
    filename: str,
    allowed_extensions: set[str] | None = None,
) -> tuple[bool, str]:
    """Return (ok, reason).

    Pass *allowed_extensions* to restrict to a specific upload type.
    If None, only the blocklist is applied (any safe extension is accepted).
    """
    if not filename or not filename.strip():
        return False, "Filename is empty"

    basename = os.path.basename(filename)

    # Path traversal
    if _TRAVERSAL_RE.search(filename):
        return False, "Path traversal not allowed"

    # Null bytes
    if "\x00" in filename:
        return False, "Null byte in filename"

    ext = os.path.splitext(basename)[1].lower()
    name_lower = basename.lower()

    # Blocked extension check
    if ext in BLOCKED_EXTENSIONS:
        return False, f"File type '{ext}' is not permitted"

    # Blocked filename pattern check
    for pattern in _BLOCKED_RE:
        if pattern.search(name_lower):
            return False, f"Filename '{basename}' matches a blocked pattern"

    # Allowlist check (when supplied)
    if allowed_extensions is not None and ext not in allowed_extensions:
        pretty = ", ".join(sorted(allowed_extensions))
        return False, f"File type '{ext}' is not allowed here. Accepted: {pretty}"

    return True, ""


def safe_filename(filename: str) -> str:
    """Sanitise a filename: strip path components, replace dangerous chars."""
    basename = os.path.basename(filename)
    # Replace anything that isn't alphanumeric, dot, dash, or underscore
    clean = re.sub(r"[^\w.\-]", "_", basename)
    # Collapse multiple underscores
    clean = re.sub(r"_+", "_", clean).strip("_")
    return clean or "upload"
