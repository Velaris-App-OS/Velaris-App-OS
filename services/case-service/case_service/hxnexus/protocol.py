"""HxNexus — pluggable LLM backend and vector store protocols.

Every backend (Ollama, OpenAI, Anthropic) and every vector store
implementation satisfies these structural protocols.
"""
from __future__ import annotations

from typing import Protocol


class LLMBackend(Protocol):
    """Pluggable LLM backend for HxNexus."""

    backend_name: str   # "ollama" | "openai" | "anthropic"

    @property
    def available(self) -> bool:
        """False when credentials or service are absent — callers skip gracefully."""
        ...

    async def complete(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> str:
        """Return a text completion. Empty string on failure."""
        ...

    async def embed(self, text: str) -> list[float]:
        """Return an embedding vector. Empty list on failure."""
        ...


class VectorStore(Protocol):
    """Pluggable vector store for document chunk similarity search."""

    async def upsert(
        self,
        chunk_id: str,
        text: str,
        embedding: list[float],
        metadata: dict,
    ) -> None:
        """Insert or update a chunk."""
        ...

    async def query(
        self,
        embedding: list[float],
        top_k: int = 5,
        filter_metadata: dict | None = None,
    ) -> list[dict]:
        """Return top-k chunks sorted by cosine similarity. Each dict has keys:
        chunk_id, text, score, metadata."""
        ...

    async def delete(self, chunk_id: str) -> None:
        """Remove a chunk by ID."""
        ...
