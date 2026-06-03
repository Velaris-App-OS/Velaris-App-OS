"""HxNexus — Anthropic Claude backend.

Requires ANTHROPIC_API_KEY env var. Unavailable when key is absent.
Default model: claude-haiku-4-5 (overridden by ANTHROPIC_MODEL env var).
Note: Anthropic doesn't provide an embeddings endpoint; embed() delegates
to Ollama if available, otherwise returns [].
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

_API_BASE = "https://api.anthropic.com/v1"
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_ANTHROPIC_VERSION = "2023-06-01"


class AnthropicBackend:
    backend_name = "anthropic"

    def __init__(self) -> None:
        self._api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self._model = os.getenv("ANTHROPIC_MODEL", _DEFAULT_MODEL)

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    async def complete(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 4096,
    ) -> str:
        if not self.available:
            return ""
        body: dict = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        try:
            async with httpx.AsyncClient(timeout=120.0) as c:
                r = await c.post(f"{_API_BASE}/messages", headers=self._headers(), json=body)
                r.raise_for_status()
                content = r.json().get("content", [])
                return "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        except Exception as exc:
            # SEC-6: never include API key value in logs
            log.warning("HxNexus Anthropic complete error: %s", type(exc).__name__)
            return ""

    async def embed(self, text: str) -> list[float]:
        # Anthropic has no embeddings API — fall back to Ollama
        from .ollama_backend import OllamaBackend
        return await OllamaBackend().embed(text)
