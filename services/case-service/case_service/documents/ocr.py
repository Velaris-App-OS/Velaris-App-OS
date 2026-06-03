"""OCR — pluggable interface. StubOCREngine ships in P24; real engine in P24b."""
from __future__ import annotations
from typing import Protocol


class OCREngine(Protocol):
    async def extract_text(self, data: bytes, content_type: str) -> str:
        """Return extracted text (empty if unsupported)."""
        ...


class StubOCREngine:
    """No-op OCR. Returns empty string; exists so callers can wire without a real engine."""

    async def extract_text(self, data: bytes, content_type: str) -> str:
        return ""


_engine: OCREngine | None = None


def get_ocr_engine() -> OCREngine:
    global _engine
    if _engine is None:
        _engine = StubOCREngine()
    return _engine


def set_ocr_engine(engine: OCREngine) -> None:
    """Register a real engine (used by P24b or plugins)."""
    global _engine
    _engine = engine
