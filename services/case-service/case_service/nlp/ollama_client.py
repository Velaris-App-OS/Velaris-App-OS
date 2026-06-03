"""Ollama client — talks to local LLM for structured output.

Copyright (c) 2024-2025 HELIX Contributors
SPDX-License-Identifier: BSL-1.1
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


async def generate_json(
    prompt: str,
    system: str = "",
    model: str = "llama3.2",
    url: str = "http://localhost:11434",
    timeout: float = 60.0,
) -> dict[str, Any] | None:
    """Call Ollama and parse JSON response.

    Returns None if Ollama is unavailable or response is invalid.
    """
    try:
        import httpx
    except ImportError:
        logger.warning("httpx not installed — NLP unavailable")
        return None

    full_prompt = f"{system}\n\n{prompt}" if system else prompt

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                f"{url}/api/generate",
                json={
                    "model": model,
                    "prompt": full_prompt,
                    "format": "json",
                    "stream": False,
                    "options": {"temperature": 0.3},
                },
            )
            if resp.status_code != 200:
                logger.warning("Ollama returned %d: %s", resp.status_code, resp.text[:200])
                return None
            data = resp.json()
            raw = data.get("response", "").strip()
            try:
                return json.loads(raw)
            except json.JSONDecodeError as e:
                logger.warning("Ollama JSON parse failed: %s", e)
                return None
    except Exception as e:
        logger.warning("Ollama call failed: %s", e)
        return None


async def check_ollama_available(
    url: str = "http://localhost:11434",
    timeout: float = 2.0,
) -> bool:
    """Quick health check for Ollama."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{url}/api/tags")
            return resp.status_code == 200
    except Exception:
        return False
