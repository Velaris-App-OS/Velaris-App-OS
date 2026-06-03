"""DoclingConnector — document field extraction via self-hosted Docling HTTP server."""
from __future__ import annotations

from typing import Any

import httpx

from case_service.hxbridge.protocol import ConnectorProtocol, register_connector


@register_connector("docling")
class DoclingConnector(ConnectorProtocol):
    name = "Docling Document Extractor"
    connector_type = "docling"
    schema = {
        "credentials": {},
        "config":      {"base_url": "str (Docling server URL, e.g. http://docling:5001)"},
    }

    def __init__(self, config: dict, credentials: dict):
        self._base_url = config.get("base_url", "http://localhost:5001").rstrip("/")

    async def execute(self, input_data: dict) -> dict:
        source_url  = input_data.get("source_url")
        doc_bytes   = input_data.get("document_bytes")  # optional raw bytes

        async with httpx.AsyncClient(timeout=60) as client:
            if source_url:
                resp = await client.post(
                    f"{self._base_url}/v1/extract",
                    json={"url": source_url, "fields": input_data.get("fields", [])},
                )
            elif doc_bytes:
                resp = await client.post(
                    f"{self._base_url}/v1/extract",
                    content=doc_bytes,
                    headers={"Content-Type": input_data.get("content_type", "application/pdf")},
                )
            else:
                raise ValueError("Either source_url or document_bytes must be provided")

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Docling error {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        return {
            "extracted_fields": data.get("fields", {}),
            "raw_text":         data.get("text", ""),
            "confidence":       data.get("confidence", 1.0),
            "status":           "completed",
        }

    async def test(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self._base_url}/health")
            return resp.status_code == 200
        except Exception:
            return False
