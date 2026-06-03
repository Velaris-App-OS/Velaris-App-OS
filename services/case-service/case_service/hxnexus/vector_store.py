"""HxNexus — vector store implementations.

InMemoryVectorStore: used in tests (no DB needed).
DbVectorStore: reads chunks from DB, computes cosine similarity in-process
               via numpy. Works on Postgres (JSONB) and SQLite (JSON). Scales
               to tens of thousands of chunks; swap for pgvector when needed.
"""
from __future__ import annotations

import math
import uuid
from typing import Any


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    try:
        import numpy as np
        va, vb = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
        denom = np.linalg.norm(va) * np.linalg.norm(vb)
        return float(np.dot(va, vb) / denom) if denom else 0.0
    except Exception:
        # Pure-python fallback (slow but correct)
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return dot / (na * nb) if na and nb else 0.0


# ── In-memory store (tests) ───────────────────────────────────────────

class InMemoryVectorStore:
    """Thread-local in-process store — ideal for tests."""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}

    async def upsert(self, chunk_id: str, text: str, embedding: list[float], metadata: dict) -> None:
        self._store[chunk_id] = {"text": text, "embedding": embedding, "metadata": metadata}

    async def query(
        self,
        embedding: list[float],
        top_k: int = 5,
        filter_metadata: dict | None = None,
    ) -> list[dict]:
        results = []
        for cid, item in self._store.items():
            if filter_metadata:
                if not all(item["metadata"].get(k) == v for k, v in filter_metadata.items()):
                    continue
            score = _cosine(embedding, item["embedding"])
            results.append({"chunk_id": cid, "text": item["text"], "score": score, "metadata": item["metadata"]})
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    async def delete(self, chunk_id: str) -> None:
        self._store.pop(chunk_id, None)


# ── DB-backed store (production) ──────────────────────────────────────

class DbVectorStore:
    """Reads chunks from DB; similarity computed in-process with numpy."""

    def __init__(self, session_factory) -> None:
        self._session_factory = session_factory

    async def upsert(self, chunk_id: str, text: str, embedding: list[float], metadata: dict) -> None:
        from case_service.db.models import DocumentChunkModel
        from sqlalchemy import select

        async with self._session_factory() as session:
            existing = await session.get(DocumentChunkModel, uuid.UUID(chunk_id))
            if existing:
                existing.chunk_text = text
                existing.embedding = embedding
            else:
                chunk = DocumentChunkModel(
                    id=uuid.UUID(chunk_id),
                    document_id=uuid.UUID(metadata["document_id"]) if metadata.get("document_id") else None,
                    case_id=uuid.UUID(metadata["case_id"]) if metadata.get("case_id") else None,
                    chunk_index=metadata.get("chunk_index", 0),
                    chunk_text=text,
                    embedding=embedding,
                    tenant_id=metadata.get("tenant_id"),
                )
                session.add(chunk)
            await session.commit()

    async def query(
        self,
        embedding: list[float],
        top_k: int = 5,
        filter_metadata: dict | None = None,
    ) -> list[dict]:
        from case_service.db.models import DocumentChunkModel
        from sqlalchemy import select

        async with self._session_factory() as session:
            q = select(DocumentChunkModel).where(DocumentChunkModel.embedding.isnot(None))
            if filter_metadata:
                if filter_metadata.get("tenant_id"):
                    q = q.where(DocumentChunkModel.tenant_id == filter_metadata["tenant_id"])
                if filter_metadata.get("case_id"):
                    q = q.where(DocumentChunkModel.case_id == uuid.UUID(str(filter_metadata["case_id"])))
                if filter_metadata.get("document_id"):
                    q = q.where(DocumentChunkModel.document_id == uuid.UUID(str(filter_metadata["document_id"])))
            rows = (await session.execute(q)).scalars().all()

        results = []
        for row in rows:
            emb = row.embedding or []
            score = _cosine(embedding, emb)
            results.append({
                "chunk_id": str(row.id),
                "text": row.chunk_text,
                "score": score,
                "metadata": {
                    "document_id": str(row.document_id) if row.document_id else None,
                    "case_id": str(row.case_id) if row.case_id else None,
                    "chunk_index": row.chunk_index,
                    "tenant_id": row.tenant_id,
                },
            })
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]

    async def delete(self, chunk_id: str) -> None:
        from case_service.db.models import DocumentChunkModel

        async with self._session_factory() as session:
            chunk = await session.get(DocumentChunkModel, uuid.UUID(chunk_id))
            if chunk:
                await session.delete(chunk)
                await session.commit()
