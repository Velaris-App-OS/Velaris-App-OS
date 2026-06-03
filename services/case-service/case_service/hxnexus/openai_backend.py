"""HxNexus — OpenAI-compatible LLM backend.

Requires OPENAI_API_KEY env var. Unavailable when key is absent.
Compatible with any OpenAI-API server (OpenAI, LM Studio, vLLM, etc.)
via OPENAI_BASE_URL override.
"""
from __future__ import annotations

import logging
import os

import httpx

log = logging.getLogger(__name__)

_DEFAULT_BASE = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"
_DEFAULT_EMBED_MODEL = "text-embedding-3-small"


class OpenAIBackend:
    backend_name = "openai"

    def __init__(self) -> None:
        self._api_key = os.getenv("OPENAI_API_KEY", "")
        self._base_url = os.getenv("OPENAI_BASE_URL", _DEFAULT_BASE).rstrip("/")
        self._model = os.getenv("OPENAI_MODEL", _DEFAULT_MODEL)
        self._embed_model = os.getenv("OPENAI_EMBED_MODEL", _DEFAULT_EMBED_MODEL)

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
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
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        try:
            async with httpx.AsyncClient(timeout=120.0) as c:
                r = await c.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers(),
                    json={
                        "model": self._model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                )
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as exc:
            # SEC-6: never include API key value in logs
            log.warning("HxNexus OpenAI complete error: %s", type(exc).__name__)
            return ""

    async def embed(self, text: str) -> list[float]:
        if not self.available:
            return []
        try:
            async with httpx.AsyncClient(timeout=30.0) as c:
                r = await c.post(
                    f"{self._base_url}/embeddings",
                    headers=self._headers(),
                    json={"model": self._embed_model, "input": text},
                )
                r.raise_for_status()
                return r.json()["data"][0]["embedding"]
        except Exception as exc:
            log.warning("HxNexus OpenAI embed error: %s", exc)
            return []
