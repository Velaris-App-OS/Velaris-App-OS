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
        r"refresh_token|access_token|otp|client_secret|grant)\b(["
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


class TenantTagFilter(logging.Filter):
    """Prefix a line written during a signed-in request with `[t=<tenant>]` —
    the TRUSTED tenant sign-in sets (2026-11-04), so HxLogs can show a tenant's
    admin the lines of its own requests. Lines with no request tenant (startup,
    jobs, access logs) stay untagged: the platform operator's alone."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if getattr(record, "_velaris_tagged", False):
                return True
            from .context import tenant_id_var
            t = tenant_id_var.get()
            if t:
                # One line per record: a newline in a message (text a tenant
                # sent) cannot start a forged line tagged for another tenant.
                msg = record.getMessage().replace("\r", " ").replace("\n", " \u23ce ")
                record.msg = f"[t={t}] {msg}"
                record.args = ()
            record._velaris_tagged = True
        except Exception:
            pass
        return True


def configure_logging() -> None:
    log_format = os.getenv("VELARIS_LOG_FORMAT", "console").lower()
    level = getattr(logging, os.getenv("VELARIS_LOG_LEVEL", "INFO").upper(), logging.INFO)
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
    tenant_filter = TenantTagFilter()
    for handler in logging.getLogger().handlers:
        handler.addFilter(tenant_filter)
        handler.addFilter(redact_filter)
    # uvicorn's loggers have their own handlers and do not reach the root's, so
    # the filter above never saw them: a socket's `?token=<JWT>` was written in
    # full to the service log ("WebSocket /api/v1/hxstream/ws?token=…", found
    # 2026-10-19). On the loggers themselves, it covers every record they make.
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        uv_logger = logging.getLogger(name)
        uv_logger.addFilter(redact_filter)
        for handler in uv_logger.handlers:
            handler.addFilter(redact_filter)


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
