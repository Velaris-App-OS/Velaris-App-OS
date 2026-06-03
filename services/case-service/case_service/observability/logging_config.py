from __future__ import annotations
import logging
import os
import sys
import structlog

from .context import get_context


def _inject_ctx(logger, method_name, event_dict):
    for k, v in get_context().items():
        if v is not None and k not in event_dict:
            event_dict[k] = v
    return event_dict


def configure_logging() -> None:
    log_format = os.getenv("HELIX_LOG_FORMAT", "console").lower()
    level = getattr(logging, os.getenv("HELIX_LOG_LEVEL", "INFO").upper(), logging.INFO)
    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _inject_ctx,
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


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
