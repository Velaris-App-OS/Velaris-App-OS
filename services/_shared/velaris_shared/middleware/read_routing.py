"""R5d 11b — the request side of the read/write split.

* After a successful write by a signed-in user, mark them: for the next
  `read_own_writes_seconds` their opted-in reads run on the main
  (velaris_shared/db/read_routing.py). The mark is set BEFORE the response
  leaves, so the user's very next request already sees it.
* A successful response served from an opted-in read path carries the time its
  data is as of: `X-Velaris-Data-As-Of` (where it ran is counted in the
  `velaris_db_reads_total` metric, not told to clients).

Pure ASGI: nothing is buffered. With no replica configured it marks nothing
and adds nothing.

Copyright (c) 2024-2026 Velaris Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

from velaris_shared.db.read_routing import SAFE_METHODS, mark_wrote


class ReadRoutingMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        method = scope.get("method", "GET")

        async def _send(message):
            if message["type"] == "http.response.start":
                state = scope.get("state") or {}
                if method not in SAFE_METHODS and message.get("status", 500) < 400:
                    try:
                        await mark_wrote(state.get("velaris_user_id"))
                    except Exception:  # noqa: BLE001 — never fail a response for this
                        pass
                # Only on an answer the caller was given (never on a 401/403), and
                # only the as-of time — where it ran (replica / main) is for the
                # metric, not for clients (found in the 11b audit).
                as_of = state.get("velaris_read_as_of")
                if as_of is not None and message.get("status", 500) < 400:
                    headers = list(message.get("headers") or [])
                    headers.append((b"x-velaris-data-as-of", as_of.isoformat().encode()))
                    message = {**message, "headers": headers}
            await send(message)

        return await self.app(scope, receive, _send)
