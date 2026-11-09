"""HxNexus — Velaris AI Copilot (P30)."""
from .service import next_best_action, qa_over_documents, chat, index_document
from velaris_shared.llm.factory import get_llm_backend

__all__ = ["next_best_action", "qa_over_documents", "chat", "index_document", "get_llm_backend"]
