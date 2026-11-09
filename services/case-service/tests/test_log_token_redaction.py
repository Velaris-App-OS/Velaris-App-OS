"""A socket's `?token=<JWT>` must not reach the service log.

uvicorn's loggers have their own handlers, so the root-level RedactFilter never
saw their lines, and "WebSocket /api/v1/hxstream/ws?token=eyJ…" was written in
full (2026-10-19). configure_logging now filters those loggers too.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
import io
import logging

import pytest

from velaris_shared.observability.logging_config import configure_logging

JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLXBhcnQ"


@pytest.mark.parametrize("name, fmt, args", [
    ("uvicorn.error", '%s - "WebSocket %s" [accepted]', ("127.0.0.1:0", f"/api/v1/hxstream/ws?token={JWT}")),
    ("uvicorn.access", '%s - "%s %s HTTP/%s" %d', ("127.0.0.1:0", "GET", f"/api/v1/x?token={JWT}", "1.1", 200)),
])
def test_uvicorn_lines_are_scrubbed(name, fmt, args):
    log = logging.getLogger(name)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    log.addHandler(handler)          # a handler of its own, as uvicorn has
    try:
        configure_logging()
        log.warning(fmt, *args)
    finally:
        log.removeHandler(handler)
    out = stream.getvalue()
    assert JWT not in out and "eyJ" not in out
    assert "REDACTED" in out


def test_audit_record_never_keeps_a_secret_query_value():
    from velaris_shared.middleware.audit import _safe_query

    out = _safe_query(f"token={JWT}&grant=v1.abc.123.sig&case_id=42&code=xyz")
    assert JWT not in out and "v1.abc.123.sig" not in out and "xyz" not in out
    assert "case_id=42" in out
    assert out.count("[REDACTED]") == 3


def test_document_grant_is_scrubbed_from_service_log_lines():
    from velaris_shared.observability.logging_config import _scrub_text

    assert "SECRET" not in _scrub_text("GET /api/v1/documents/x/download?grant=SECRET HTTP/1.1")
