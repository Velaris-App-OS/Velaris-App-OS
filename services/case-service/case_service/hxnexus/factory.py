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


# ── Group E: AI egress guard (layers 1–3) ─────────────────────────────────────

def _is_local_url(url: str) -> bool:
    return any(h in (url or "") for h in ("localhost", "127.0.0.1", "[::1]", "0.0.0.0"))


class EgressGuardedBackend:
    """Wraps an external backend so embeddings never leave the platform.

    embed() is always delegated to the local Ollama backend — indexing a
    document embeds every chunk, so without this the entire document leaves
    the platform at index time.

    Group H escalation ladder (layer 4): complete() answers with the LOCAL
    model first; the external provider is consulted only when the local
    model is unavailable / returns nothing, or when the caller explicitly
    opted in for this call (`prefer_external = True`, set per request by the
    service layer). `last_route` records which side answered — the service
    uses it for the external-AI disclosure and the egress audit.

    Note: chunks embedded locally are not comparable with chunks embedded
    by an external model. Deployments that indexed documents under
    ai_egress_policy=full must re-index after switching to minimized.
    """
    is_external = True
    embeddings_local = True

    def __init__(self, external, local) -> None:
        self._external = external
        self._local = local
        self.prefer_external = False        # per-request opt-in, set by the service
        self.last_route = "local"           # "local" | "external" — who answered last
        self.suppress_generic_audit = False  # qa/chat write their own rich audit rows

    def __getattr__(self, name):
        return getattr(self._external, name)

    async def embed(self, text: str) -> list[float]:
        return await self._local.embed(text)

    async def complete(self, prompt: str, **kwargs):
        if not self.prefer_external and getattr(self._local, "available", False):
            try:
                answer = await self._local.complete(prompt, **kwargs)
                if answer and answer.strip():
                    self.last_route = "local"
                    return answer
                log.info("egress ladder: local answer empty — escalating to external")
            except Exception as exc:
                log.info("egress ladder: local completion failed (%s) — escalating", exc)
        self.last_route = "external"
        if not self.suppress_generic_audit:
            # Catch-all audit for every other caller (generate_json: NLP
            # Builder, Scout, BPM importer, …) — flushed to SecurityEvents
            # by the watcher. qa/chat suppress this and write richer rows.
            from .egress_audit import queue_egress
            queue_egress(
                purpose="completion",
                provider=getattr(self._external, "backend_name", "external"),
                bytes_out=len(prompt or "") + len(kwargs.get("system", "") or ""),
            )
        return await self._external.complete(prompt, **kwargs)


class GuardedBackend:
    """§5.3 universal LLM hardening around *any* backend's ``complete()``.

    Applied to whatever ``get_llm_backend()`` resolves (local or external), so
    every caller — chat, generate_json, blueprints, autodoc, decision points,
    polyglot — is protected at one place, not just the chat edge:

      * **model-DoS size cap** — total prompt+system chars vs the configured
        ceiling; raises before the model is reached;
      * **prompt-injection scan** — flagged prompts are queued as
        ``ai.prompt_flagged`` SecurityEvents (signal, non-blocking — the
        governed system prompt remains the actual defense);
      * **output scrub** — secret-like patterns redacted from every response.

    Every other attribute is proxied transparently in BOTH directions, so the
    egress ladder's per-request flags (``prefer_external``, ``last_route``,
    ``suppress_generic_audit``) still land on the inner backend.
    """

    def __init__(self, inner) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_inner"), name, value)

    async def complete(self, prompt: str, **kwargs):
        from . import guard
        from .egress_audit import queue_prompt_flagged
        system = kwargs.get("system", "") or ""
        guard.validate_prompt_size(prompt, system)          # model-DoS — raises
        scan = guard.scan_input(prompt or "")               # injection signal
        if scan.flagged:
            queue_prompt_flagged(signals=scan.signals)
        raw = await self._inner.complete(prompt, **kwargs)
        return guard.scrub_output(raw) if isinstance(raw, str) else raw


def _local_backend(s) -> OllamaBackend:
    b = OllamaBackend(
        url=s.ai_ollama_url,
        model=s.ai_ollama_model,
        embed_model=s.ai_ollama_embed_model,
    )
    b.is_external = False
    return b


def _apply_egress_policy(backend, s):
    """Enforce HELIX_CASE_AI_EGRESS_POLICY on the resolved backend.

      local_only — external providers refused; local Ollama is used instead
      minimized  — external completions allowed, embeddings stay local (default)
      full       — external provider used as-is (explicit opt-in)
    """
    if not getattr(backend, "is_external", False):
        return backend

    policy = (s.ai_egress_policy or "minimized").lower().strip()

    if policy == "local_only":
        log.warning(
            "ai_egress_policy=local_only: refusing external provider '%s' — using local Ollama",
            getattr(backend, "backend_name", "?"),
        )
        return _local_backend(s)

    if policy == "full":
        return backend

    if policy != "minimized":
        log.warning("unknown ai_egress_policy '%s' — treating as 'minimized'", policy)
    return EgressGuardedBackend(backend, _local_backend(s))


def get_llm_backend():
    """Project-wide LLM backend, wrapped in the §5.3 universal guard.

    All AI callers go through here, so wrapping the resolved backend in
    :class:`GuardedBackend` enforces model-DoS limits, injection signalling,
    and output scrubbing on every completion — local or external.
    """
    return GuardedBackend(_resolve_llm_backend())


def _resolve_llm_backend():
    """Resolve the raw backend.

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
            b.is_external = True
            return _apply_egress_policy(b, s)

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
        # `custom` pointed at a localhost URL (LM Studio, vLLM, …) stays local
        b.is_external = not (provider == "custom" and _is_local_url(b._base_url))
        return _apply_egress_policy(b, s)

    # ── 2. Legacy provider-specific vars ─────────────────────────────────────
    choice = s.ai_backend.lower()

    if choice == "openai" or (choice == "auto" and s.ai_openai_api_key):
        b = OpenAIBackend()
        b._api_key     = s.ai_openai_api_key or b._api_key
        b._model       = s.ai_openai_model
        b._embed_model = s.ai_openai_embed_model
        b.is_external  = not _is_local_url(b._base_url)
        return _apply_egress_policy(b, s)

    if choice == "anthropic" or (choice == "auto" and s.ai_anthropic_api_key):
        b = AnthropicBackend()
        b._api_key = s.ai_anthropic_api_key or b._api_key
        b._model   = s.ai_anthropic_model
        b.is_external = True
        return _apply_egress_policy(b, s)

    # ── 3. Ollama fallback ────────────────────────────────────────────────────
    return _local_backend(s)


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
        "egress_policy": (s.ai_egress_policy or "minimized").lower().strip(),
        "is_external": getattr(llm, "is_external", False),
        "embeddings_local": getattr(llm, "embeddings_local", not getattr(llm, "is_external", False)),
        "config": {
            "ai_backend": s.ai_backend,
            "ollama_url": s.ai_ollama_url,
            "ollama_model": s.ai_ollama_model,
            "ollama_embed_model": s.ai_ollama_embed_model,
            "openai_configured": bool(s.ai_openai_api_key),
            "anthropic_configured": bool(s.ai_anthropic_api_key),
        },
    }
