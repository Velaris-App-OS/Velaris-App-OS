from __future__ import annotations
import logging
import os
import re
import sys
import structlog

from .context import get_context


def _inject_ctx(logger, method_name, event_dict):
    for k, v in get_context().items():
        if v is not None and k not in event_dict:
            event_dict[k] = v
    return event_dict


# ── D3: sensitive-data redaction ──────────────────────────────────────────────

_SENSITIVE_KEYS = {
    "password", "passwd", "secret", "token", "authorization", "api_key",
    "apikey", "refresh_token", "access_token", "otp", "mfa_code",
    "client_secret", "private_key", "cookie", "set-cookie", "credentials",
}

_VALUE_PATTERNS = [
    # password=..., token: "...", api_key='...' etc. in free-text messages
    (re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key|authorization|"
        r"refresh_token|access_token|otp|client_secret)\b(["
        r"'\"]?\s*[:=]\s*)\S+"),
     r"\1\2[REDACTED]"),
    # Bearer tokens
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"), "Bearer [REDACTED]"),
    # Anything that looks like a JWT
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
     "[REDACTED-JWT]"),
]


def _scrub_text(text: str) -> str:
    for pattern, replacement in _VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_event_dict(logger, method_name, event_dict):
    """structlog processor — redact sensitive keys and scrub string values."""
    _redact_mapping(event_dict)
    return event_dict


def _redact_mapping(mapping: dict) -> None:
    for key, value in list(mapping.items()):
        if key != "event" and key.lower() in _SENSITIVE_KEYS:
            mapping[key] = "[REDACTED]"
        elif isinstance(value, dict):
            _redact_mapping(value)
        elif isinstance(value, str):
            mapping[key] = _scrub_text(value)


class RedactFilter(logging.Filter):
    """stdlib filter — scrubs secrets out of plain log lines (incl. uvicorn)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            scrubbed = _scrub_text(message)
            if scrubbed != message:
                record.msg = scrubbed
                record.args = ()
        except Exception:
            pass  # never let redaction break logging itself
        return True


def configure_logging() -> None:
    log_format = os.getenv("HELIX_LOG_FORMAT", "console").lower()
    level = getattr(logging, os.getenv("HELIX_LOG_LEVEL", "INFO").upper(), logging.INFO)
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _inject_ctx,
        _redact_event_dict,  # D3: redact secrets before rendering
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if log_format == "json"
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )
    structlog.configure(
        processors=processors,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    # Include asctime so every stdlib log line (incl. uvicorn access logs) carries a timestamp
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
        level=level,
        force=True,
    )
    # D3: scrub secrets from every stdlib log line (incl. uvicorn access logs)
    redact_filter = RedactFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(redact_filter)


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
