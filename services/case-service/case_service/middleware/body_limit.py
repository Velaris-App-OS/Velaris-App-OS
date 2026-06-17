"""Request body size limit middleware (D2).

Three-tier limit:
  - max_body_bytes           (default 10 MB)  — all requests (JSON payloads)
  - max_upload_bytes         (default 25 MB)  — multipart/form-data uploads
    (documents, portal, HxNexus, importer, Scout)
  - max_migrate_upload_bytes (default 200 MB) — multipart uploads on
    hxmigrate routes only; BPM vendor migration exports are the one
    legitimately large payload. hxmigrate's own SEC-7 check still enforces
    100 MB per file inside the request — this tier is the outer transport
    cap (file + form fields + multipart framing overhead).

Enforcement is two-layered so it cannot be bypassed:
  1. Content-Length header check — rejects oversized requests with 413
     before a single body byte is read.
  2. Streaming byte counter — requests without Content-Length (chunked
     transfer encoding) are counted as chunks arrive and aborted at the
     limit. Omitting the header does not evade the cap.

Pure ASGI middleware (not BaseHTTPMiddleware) so the receive channel can be
wrapped without buffering the body.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging

from starlette.datastructures import Headers

logger = logging.getLogger(__name__)


class _BodyTooLarge(Exception):
    pass


class BodyLimitMiddleware:
    def __init__(
        self,
        app,
        max_body_bytes: int = 10 * 1024 * 1024,
        max_upload_bytes: int = 25 * 1024 * 1024,
        max_migrate_upload_bytes: int = 200 * 1024 * 1024,
        migrate_path_prefix: str = "/api/v1/hxmigrate",
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.max_upload_bytes = max_upload_bytes
        self.max_migrate_upload_bytes = max_migrate_upload_bytes
        self.migrate_path_prefix = migrate_path_prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = Headers(scope=scope)
        content_type = headers.get("content-type", "")
        if content_type.startswith("multipart/"):
            limit = (
                self.max_migrate_upload_bytes
                if scope.get("path", "").startswith(self.migrate_path_prefix)
                else self.max_upload_bytes
            )
        else:
            limit = self.max_body_bytes

        # Layer 1: declared Content-Length
        content_length = headers.get("content-length")
        if content_length and content_length.isdigit() and int(content_length) > limit:
            logger.warning(
                "body limit: rejected %s %s (declared %s bytes > %d limit)",
                scope.get("method"), scope.get("path"), content_length, limit,
            )
            return await self._reject(send, limit)

        # Layer 2: count streamed bytes (covers chunked / missing Content-Length)
        received = 0
        response_started = False  # app began its own response
        rejected = False          # we already sent the 413

        async def recv_wrapper():
            nonlocal received, rejected
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    logger.warning(
                        "body limit: aborted streamed %s %s (exceeded %d bytes)",
                        scope.get("method"), scope.get("path"), limit,
                    )
                    # Respond 413 here, before raising: the framework converts
                    # exceptions raised during body parsing into its own 400,
                    # which send_wrapper below will then suppress.
                    if not response_started and not rejected:
                        rejected = True
                        await self._reject(send, limit)
                    raise _BodyTooLarge()
            return message

        async def send_wrapper(message):
            nonlocal response_started
            if rejected:
                return  # 413 already sent — drop the framework's response
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, recv_wrapper, send_wrapper)
        except _BodyTooLarge:
            if not rejected and not response_started:
                await self._reject(send, limit)

    @staticmethod
    async def _reject(send, limit: int) -> None:
        body = json.dumps({
            "detail": f"Request body too large. Limit is {limit // (1024 * 1024)} MB.",
        }).encode()
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
