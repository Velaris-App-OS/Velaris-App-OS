"""Document management — service, preview, OCR."""
from .service import DocumentService
from .preview import generate_preview, PREVIEW_SUPPORTED_TYPES
from .ocr import OCREngine, StubOCREngine, get_ocr_engine

__all__ = [
    "DocumentService",
    "generate_preview", "PREVIEW_SUPPORTED_TYPES",
    "OCREngine", "StubOCREngine", "get_ocr_engine",
]
