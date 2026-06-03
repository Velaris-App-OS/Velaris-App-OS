"""HxNexus — Ollama LLM backend (default, open-source).

Reuses the Ollama HTTP API already wired in P15 NLP.
Adds /api/embeddings support for RAG.
"""
from __future__ import annotations

import json
import logging
import os

import httpx

log = logging.getLogger(__name__)


class OllamaBackend:
    backend_name = "ollama"

    def __init__(
        self,
        url: str | None = None,
        model: str | None = None,
        embed_model: str | None = None,
    ) -> None:
        self._url = url or os.getenv("OLLAMA_URL", "http://localhost:11434")
        self._model = model or os.getenv("OLLAMA_MODEL", "llama3.2")
        # Many Ollama installs use nomic-embed-text for embeddings
        self._embed_model = embed_model or os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
        self._available: bool | None = None   # lazily checked

    @property
    def available(self) -> bool:
        # True by default; real check done via check_available() before responses
        return True

    async def check_available(self) -> bool:
        """Async liveness check — call this from status endpoints."""
        try:
            async with httpx.AsyncClient(timeout=2.0) as c:
                r = await c.get(f"{self._url}/api/tags")
                return r.status_code == 200
        except Exception:
            return False

    async def complete(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        try:
            body: dict = {
                "model": self._model,
                "prompt": f"{system}\n\n{prompt}" if system else prompt,
                "stream": False,
                "options": {"temperature": temperature, "num_predict": max_tokens},
            }
            async with httpx.AsyncClient(timeout=180.0) as c:
                r = await c.post(f"{self._url}/api/generate", json=body)
                r.raise_for_status()
                return r.json().get("response", "").strip()
        except Exception as exc:
            log.warning("HxNexus Ollama complete error: %s", type(exc).__name__)
            return ""

    async def embed(self, text: str) -> list[float]:
        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(
                    f"{self._url}/api/embeddings",
                    json={"model": self._embed_model, "prompt": text},
                )
                r.raise_for_status()
                return r.json().get("embedding", [])
        except Exception as exc:
            log.warning("HxNexus Ollama embed error: %s", exc)
            return []
