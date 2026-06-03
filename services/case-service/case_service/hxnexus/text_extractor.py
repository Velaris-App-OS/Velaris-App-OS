"""HxNexus — document text extractor.

Supported formats:
  PDF  — via pypdf; text layers extracted; image-only pages yield
         no text (use a vision-capable backend: Anthropic/OpenAI/llava for OCR).
  DOCX — via python-docx; paragraph text concatenated.
  TXT / HTML / XML — raw UTF-8 decode.
"""
from __future__ import annotations

import io
import logging

log = logging.getLogger(__name__)


def extract_text(data: bytes, content_type: str) -> str:
    """Extract plain text from document bytes. Returns empty string on failure."""
    ct = (content_type or "").lower()

    if "pdf" in ct:
        try:
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(data))
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n".join(pages).strip()
        except Exception as exc:
            log.warning("HxNexus PDF extraction failed: %s", exc)
            return ""

    if "wordprocessingml" in ct or ct.endswith(".docx"):
        try:
            import docx
            d = docx.Document(io.BytesIO(data))
            return "\n".join(p.text for p in d.paragraphs if p.text.strip()).strip()
        except Exception as exc:
            log.warning("HxNexus DOCX extraction failed: %s", exc)
            return ""

    if "text" in ct or "html" in ct or "xml" in ct:
        try:
            return data.decode("utf-8", errors="ignore").strip()
        except Exception:
            return ""

    return ""
