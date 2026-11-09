"""Content-Security-Policy reports from Studio (2026-10-29).

Studio sends a policy (`studio/csp.config.ts`); a browser posts here what the
policy blocked, or — in report-only mode, the default — would have blocked.
That is how an operator knows it is safe to turn `VELARIS_STUDIO_CSP=enforce` on.

Anonymous by necessity: browsers send these reports with no credentials. So
the body is capped, the rate is capped, only a few named fields are logged,
and never a query string or fragment — some Studio URLs carry a token.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response

logger = logging.getLogger("velaris.studio.csp")
router = APIRouter(prefix="/csp-reports", tags=["csp-reports"])

MAX_BODY = 16_384
#: Reports logged per minute, per process; the rest are dropped (counted).
PER_MINUTE = 120
_window = {"start": 0.0, "count": 0, "dropped": 0}


def _clean(uri: object) -> str:
    """A URI without its query or fragment; a keyword ("inline", "eval") as is."""
    text = str(uri or "")[:300]
    if "://" not in text:
        return text.split("?")[0].split("#")[0][:60]
    try:
        p = urlsplit(text)
        return f"{p.scheme}://{p.netloc}{p.path}"[:200]
    except ValueError:
        return "?"


def _allow() -> bool:
    now = time.monotonic()
    if now - _window["start"] >= 60:
        if _window["dropped"]:
            logger.warning("studio csp: %d report(s) dropped over the rate limit", _window["dropped"])
        _window.update(start=now, count=0, dropped=0)
    _window["count"] += 1
    if _window["count"] > PER_MINUTE:
        _window["dropped"] += 1
        return False
    return True


@router.post("", status_code=204)
async def csp_report(request: Request) -> Response:
    body = await request.body()
    if len(body) > MAX_BODY or not _allow():
        return Response(status_code=204)
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return Response(status_code=204)
    items = data if isinstance(data, list) else [data]
    for item in items[:5]:
        r = item.get("csp-report") if isinstance(item, dict) and "csp-report" in item else \
            (item.get("body") if isinstance(item, dict) else None)
        if not isinstance(r, dict):
            continue
        directive = r.get("effective-directive") or r.get("effectiveDirective") or r.get("violated-directive") or ""
        blocked = r.get("blocked-uri") or r.get("blockedURL") or ""
        page = r.get("document-uri") or r.get("documentURL") or ""
        mode = r.get("disposition") or ""
        logger.warning("studio csp %s: %s blocked %s on %s", _clean(mode) or "violation",
                       _clean(directive), _clean(blocked), _clean(page))
    return Response(status_code=204)
