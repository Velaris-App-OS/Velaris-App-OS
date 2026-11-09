"""HxNexus / shared AI factory.

Single source of truth for the project-wide LLM backend.
All AI features (HxNexus, NLP Builder, Scout AI) call get_llm_backend()
and get the backend configured via VELARIS_CASE_AI_* env vars.

SIMPLE usage — set just these in .env, zero code changes needed:
  VELARIS_CASE_AI_PROVIDER = groq          (or mistral | gemini | together | openai | anthropic | ollama | custom)
  VELARIS_CASE_AI_API_KEY  = gsk_xxxx
  VELARIS_CASE_AI_MODEL    = llama-3.1-70b-versatile

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
    "azure":        "",   # requires full VELARIS_CASE_AI_BASE_URL
}


# ── Group E: AI egress guard (layers 1–3) ─────────────────────────────────────

def _is_local_url(url: str) -> bool:
    return any(h in (url or "") for h in ("localhost", "127.0.0.1", "[::1]", "0.0.0.0"))


def detect_provider_misconfig(s) -> dict[str, str] | None:
    """Is the configured external provider missing something it cannot work without?

    Returns ``{"provider", "missing", "detail"}`` describing the fault, or
    ``None`` when the configuration is usable.

    This exists because the failure it catches is silent and expensive. Setting
    ``AI_PROVIDER`` without ``AI_API_KEY`` used to build a fully-formed external
    backend holding an empty key: every completion then failed at the provider
    with a 401, which reads like a model outage rather than a typo in `.env`,
    while the status screen reported the provider as configured and a working
    local Ollama was never consulted.

    Deliberately a **pure function** of settings rather than a flag stashed on
    the module: the resolver and the status endpoint must not be able to
    disagree about whether the platform is misconfigured, and module-level
    mutable state between them is exactly how they would.
    """
    provider = (s.ai_provider or "").lower().strip()
    if not provider or provider == "ollama":
        return None

    base_url = (s.ai_base_url or PROVIDER_URLS.get(provider, "")).strip()

    # `custom` is the local-runtime escape hatch (LM Studio, vLLM, llama.cpp).
    # It needs an address, not a credential — requiring a key here would break
    # the keyless local servers it exists to support.
    if provider == "custom":
        if not base_url:
            return {
                "provider": provider,
                "missing": "VELARIS_CASE_AI_BASE_URL",
                "detail": "provider 'custom' has no built-in address, so a base URL must be given.",
            }
        return None

    if not (s.ai_api_key or "").strip():
        return {
            "provider": provider,
            "missing": "VELARIS_CASE_AI_API_KEY",
            "detail": f"provider '{provider}' is external and cannot authenticate without a key.",
        }

    # Anthropic ships its own SDK and knows its own endpoint; everyone else is
    # resolved through PROVIDER_URLS. An unrecognised name lands here with no
    # address — and must not be allowed to proceed, because OpenAIBackend would
    # fall back to its own default and post the key to api.openai.com under
    # some other provider's name.
    if provider != "anthropic" and not base_url:
        return {
            "provider": provider,
            "missing": "VELARIS_CASE_AI_BASE_URL",
            "detail": (
                f"'{provider}' is not a known provider, so its address cannot be resolved. "
                "Set a base URL explicitly, or correct the provider name."
            ),
        }

    return None


def _trusted_tenant():
    """The signed-in caller's tenant for the AI-call record (2026-11-04); None
    outside a request (a job), which the views read as the default tenant's."""
    from velaris_shared.observability.context import tenant_id_var
    return tenant_id_var.get()


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

    After each ``complete()`` the backend also carries ``last_usage`` — the
    token counts and latency the chokepoint measured — for F-02 to price. Like
    ``last_route`` it describes the MOST RECENT call and nothing more, so read
    it immediately; ``get_llm_backend()`` builds a fresh wrapper per caller, so
    two requests do not share one.
    """

    def __init__(self, inner) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, "_inner"), name)

    def __setattr__(self, name, value):
        setattr(object.__getattribute__(self, "_inner"), name, value)

    async def complete(self, prompt: str, **kwargs):
        import time

        from . import guard, llmops
        from .egress_audit import queue_prompt_flagged
        system = kwargs.get("system", "") or ""
        guard.validate_prompt_size(prompt, system)          # model-DoS — raises
        scan = guard.scan_input(prompt or "")               # injection signal
        if scan.flagged:
            queue_prompt_flagged(signals=scan.signals)

        # #32 LLMOps: this is the one chokepoint every AI call passes through,
        # so it is the only place a per-call metric can be complete. Failures
        # are recorded too — an error rate is half the point of the metric.
        started = time.perf_counter()
        # Popped, not read: `purpose` is a metrics label, and forwarding it to
        # a backend that does not accept it would turn labelling into a crash.
        purpose = kwargs.pop("purpose", None) or "completion"
        try:
            raw = await self._inner.complete(prompt, **kwargs)
        except Exception as exc:
            self.last_usage = llmops.queue_call(
                tenant_id=_trusted_tenant(),
                **self._call_identity(),
                purpose=purpose,
                prompt_chars=len(prompt or "") + len(system),
                completion_chars=0,
                latency_ms=(time.perf_counter() - started) * 1000.0,
                status="error",
                error_type=type(exc).__name__,
            )
            raise

        out = guard.scrub_output(raw) if isinstance(raw, str) else raw
        # F-02: the token counts measured HERE are stashed for the caller, the
        # same way `last_route` already is. A caller that re-derived them from
        # the text it holds would produce a second, quietly different number for
        # the same completion — and for case Q&A it would be wrong outright,
        # since the ledger commits to the question while the model was sent the
        # question plus every case source.
        #
        # Set on BOTH paths, including the failure above: leaving the previous
        # call's numbers in place would let a failed completion be priced as the
        # successful one before it.
        self.last_usage = llmops.queue_call(
                tenant_id=_trusted_tenant(),
            **self._call_identity(),
            purpose=purpose,
            prompt_chars=len(prompt or "") + len(system),
            completion_chars=len(out) if isinstance(out, str) else 0,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )
        return out

    def _call_identity(self) -> dict:
        """Which model actually answered — resolved after the call, since the
        egress ladder may have escalated local → external mid-flight."""
        ident = active_model(self)
        return {
            "provider": ident["provider"],
            "model": ident["model"],
            "route": ident["route"],
        }


def _local_backend(s) -> OllamaBackend:
    b = OllamaBackend(
        url=s.ai_ollama_url,
        model=s.ai_ollama_model,
        embed_model=s.ai_ollama_embed_model,
    )
    b.is_external = False
    return b


def _apply_egress_policy(backend, s):
    """Enforce VELARIS_CASE_AI_EGRESS_POLICY on the resolved backend.

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
      1. Universal vars (VELARIS_CASE_AI_PROVIDER / API_KEY / MODEL) — preferred
      2. Legacy provider-specific vars — backwards compat
      3. Ollama — default fallback
    """
    from velaris_shared.config import get_settings
    s = get_settings()

    # ── 1. Universal provider vars ────────────────────────────────────────────
    provider = (s.ai_provider or "").lower().strip()
    if provider and provider not in ("", "ollama"):
        # A provider that cannot possibly work is not used. Falling back keeps
        # AI answering from the local model instead of 401-ing at every call
        # site; raising here would turn one wrong env var into a 500 on every
        # page that touches AI, since this resolver sits on the request path.
        fault = detect_provider_misconfig(s)
        if fault:
            log.error(
                "VELARIS_CASE_AI_PROVIDER=%s but %s is not set — %s "
                "Falling back to the local Ollama backend.",
                fault["provider"], fault["missing"], fault["detail"],
            )
            return _local_backend(s)

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

        # All other known providers use the OpenAI-compatible API.
        # `base_url` is guaranteed non-empty here by detect_provider_misconfig.
        base_url = s.ai_base_url or PROVIDER_URLS.get(provider, "")

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


def active_model(llm: Any = None) -> dict[str, str]:
    """Identity of the model that served the most recent ``complete()``.

    GDPR Article 22 requires an AI decision to name the model that produced it,
    and "whatever is configured" is not the same answer: the egress ladder
    answers locally first and escalates to the external provider only when the
    local model cannot. `LocalFirstBackend.last_route` records which side
    actually replied, so this reports that side rather than the configured
    default.

    Call it immediately after the completion whose provenance you want.
    Returns empty-ish strings rather than raising — a decision must never fail
    because its audit metadata could not be resolved.
    """
    try:
        llm = llm if llm is not None else get_llm_backend()
        inner = getattr(llm, "_inner", llm)          # unwrap GuardedBackend
        route = getattr(inner, "last_route", None)   # set by LocalFirstBackend
        if route == "local":
            target = getattr(inner, "_local", inner)
        elif route == "external":
            target = getattr(inner, "_external", inner)
        else:
            target = inner
        return {
            "provider": str(getattr(target, "backend_name", type(target).__name__)),
            "model": str(getattr(target, "_model", "") or "unknown"),
            "route": str(route or "direct"),
        }
    except Exception:  # provenance is never worth failing a decision over
        return {"provider": "unknown", "model": "unknown", "route": "unknown"}


async def generate_json(
    prompt: str,
    system: str = "",
    temperature: float = 0.3,
    max_tokens: int = 4096,
    llm: Any = None,
) -> dict[str, Any] | None:
    """Generate structured JSON from any backend.

    Used by NLP Builder and Scout AI so they benefit from
    whatever backend is configured (Ollama, OpenAI, Anthropic).
    Returns None if backend is unavailable or response is not valid JSON.

    `llm` lets a caller supply — and therefore KEEP — the backend. Without it
    this function builds one and discards it, so the caller cannot read
    `last_route` or `last_usage` afterwards: `active_model()` with no argument
    then resolves a *fresh* backend that has answered nothing, and reports the
    CONFIGURED model rather than the one that actually replied. Optional, so
    every existing caller is unchanged.
    """
    llm = llm if llm is not None else get_llm_backend()

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
    from velaris_shared.config import get_settings
    s = get_settings()
    llm = get_llm_backend()
    # Surfaced, not merely logged: the whole failure mode this guards against is
    # a status screen that reports a provider as configured while every call to
    # it fails. `backend` below correctly reads "ollama" after a fallback, which
    # tells an operator what is answering but not that their intended provider
    # was dropped, or why.
    fault = detect_provider_misconfig(s)
    return {
        "backend": llm.backend_name,
        "egress_policy": (s.ai_egress_policy or "minimized").lower().strip(),
        "is_external": getattr(llm, "is_external", False),
        "embeddings_local": getattr(llm, "embeddings_local", not getattr(llm, "is_external", False)),
        "misconfigured": fault,
        "config": {
            "ai_backend": s.ai_backend,
            "ollama_url": s.ai_ollama_url,
            "ollama_model": s.ai_ollama_model,
            "ollama_embed_model": s.ai_ollama_embed_model,
            "openai_configured": bool(s.ai_openai_api_key),
            "anthropic_configured": bool(s.ai_anthropic_api_key),
        },
    }
