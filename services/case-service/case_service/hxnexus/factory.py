"""HxNexus / shared AI factory.

Single source of truth for the project-wide LLM backend.
All AI features (HxNexus, NLP Builder, Scout AI) call get_llm_backend()
and get the backend configured via HELIX_CASE_AI_* env vars.

SIMPLE usage — set just these in .env, zero code changes needed:
  HELIX_CASE_AI_PROVIDER = groq          (or mistral | gemini | together | openai | anthropic | ollama | custom)
  HELIX_CASE_AI_API_KEY  = gsk_xxxx
  HELIX_CASE_AI_MODEL    = llama-3.1-70b-versatile

The factory auto-resolves the base URL for known providers.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .ollama_backend import OllamaBackend
from .openai_backend import OpenAIBackend
from .anthropic_backend import AnthropicBackend

log = logging.getLogger(__name__)

# Known OpenAI-compatible provider base URLs.
# To add a new provider: add one line here, then set env vars — no code elsewhere.
PROVIDER_URLS: dict[str, str] = {
    "openai":       "https://api.openai.com/v1",
    "groq":         "https://api.groq.com/openai/v1",
    "mistral":      "https://api.mistral.ai/v1",
    "gemini":       "https://generativelanguage.googleapis.com/v1beta/openai",
    "together":     "https://api.together.xyz/v1",
    "fireworks":    "https://api.fireworks.ai/inference/v1",
    "perplexity":   "https://api.perplexity.ai",
    "deepseek":     "https://api.deepseek.com/v1",
    "xai":          "https://api.x.ai/v1",
    "cohere":       "https://api.cohere.ai/compatibility/v1",
    "azure":        "",   # requires full HELIX_CASE_AI_BASE_URL
}


def get_llm_backend():
    """Return the project-wide LLM backend.

    Resolution order:
      1. Universal vars (HELIX_CASE_AI_PROVIDER / API_KEY / MODEL) — preferred
      2. Legacy provider-specific vars — backwards compat
      3. Ollama — default fallback
    """
    from case_service.config import get_settings
    s = get_settings()

    # ── 1. Universal provider vars ────────────────────────────────────────────
    provider = (s.ai_provider or "").lower().strip()
    if provider and provider not in ("", "ollama"):
        api_key   = s.ai_api_key
        model     = s.ai_model
        embed_model = s.ai_embed_model

        # Anthropic has its own SDK — not OpenAI-compatible
        if provider == "anthropic":
            b = AnthropicBackend()
            if api_key:   b._api_key = api_key
            if model:     b._model   = model
            return b

        # All other known providers use the OpenAI-compatible API
        base_url = s.ai_base_url or PROVIDER_URLS.get(provider, "")
        if not base_url and provider == "custom":
            log.warning("HELIX_CASE_AI_PROVIDER=custom but HELIX_CASE_AI_BASE_URL is not set")

        b = OpenAIBackend()
        b._api_key     = api_key
        b._model       = model or b._model
        b._base_url    = base_url or b._base_url
        if embed_model:
            b._embed_model = embed_model
        b.backend_name = provider
        return b

    # ── 2. Legacy provider-specific vars ─────────────────────────────────────
    choice = s.ai_backend.lower()

    if choice == "openai" or (choice == "auto" and s.ai_openai_api_key):
        b = OpenAIBackend()
        b._api_key     = s.ai_openai_api_key or b._api_key
        b._model       = s.ai_openai_model
        b._embed_model = s.ai_openai_embed_model
        return b

    if choice == "anthropic" or (choice == "auto" and s.ai_anthropic_api_key):
        b = AnthropicBackend()
        b._api_key = s.ai_anthropic_api_key or b._api_key
        b._model   = s.ai_anthropic_model
        return b

    # ── 3. Ollama fallback ────────────────────────────────────────────────────
    return OllamaBackend(
        url=s.ai_ollama_url,
        model=s.ai_ollama_model,
        embed_model=s.ai_ollama_embed_model,
    )


async def generate_json(
    prompt: str,
    system: str = "",
    temperature: float = 0.3,
    max_tokens: int = 4096,
) -> dict[str, Any] | None:
    """Generate structured JSON from any backend.

    Used by NLP Builder and Scout AI so they benefit from
    whatever backend is configured (Ollama, OpenAI, Anthropic).
    Returns None if backend is unavailable or response is not valid JSON.
    """
    llm = get_llm_backend()

    available = await llm.check_available() if hasattr(llm, "check_available") else llm.available
    if not available:
        return None

    json_system = (
        (system + "\n\n" if system else "")
        + "You MUST respond with valid JSON only. No markdown, no explanation — just the JSON object."
    )

    try:
        raw = await llm.complete(prompt, system=json_system, temperature=temperature, max_tokens=max_tokens)
        if not raw:
            return None
        text = _strip_fences(raw)
        return json.loads(text)
    except (json.JSONDecodeError, Exception) as exc:
        log.warning("generate_json parse failed: %s", type(exc).__name__)
        return None


async def generate_blueprint(
    prompt: str,
    system: str = "",
    temperature: float = 0.2,
) -> dict[str, Any] | None:
    """High-token variant of generate_json for full VelarisBlueprint extraction.

    Uses 8192 tokens to handle complex BPM artifacts without truncation.
    Works with any configured backend — Ollama, Groq, OpenAI, Anthropic.
    """
    return await generate_json(prompt, system=system, temperature=temperature, max_tokens=8192)


def _strip_fences(text: str) -> str:
    """Remove markdown code fences from LLM output."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


async def check_ai_available() -> bool:
    """Quick liveness check for the configured AI backend."""
    llm = get_llm_backend()
    if hasattr(llm, "check_available"):
        return await llm.check_available()
    return llm.available


def get_ai_info() -> dict:
    """Return backend name and config for status endpoints."""
    from case_service.config import get_settings
    s = get_settings()
    llm = get_llm_backend()
    return {
        "backend": llm.backend_name,
        "config": {
            "ai_backend": s.ai_backend,
            "ollama_url": s.ai_ollama_url,
            "ollama_model": s.ai_ollama_model,
            "ollama_embed_model": s.ai_ollama_embed_model,
            "openai_configured": bool(s.ai_openai_api_key),
            "anthropic_configured": bool(s.ai_anthropic_api_key),
        },
    }
